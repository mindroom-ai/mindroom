"""OpenBB tool configuration and native implementation."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    narrow_action_enum,
    require_enabled_action,
    require_non_empty,
    resolve_bool_toggle,
)

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_OpenBBProvider = Literal["benzinga", "fmp", "intrinio", "polygon", "tiingo", "tmx", "yfinance"]
_OpenBBAction = Literal["stock_price", "search_symbol", "company_news", "company_profile", "price_targets"]


def _load_openbb_app() -> Any:  # noqa: ANN401 - openbb.obb is dynamically generated on first import
    """Import ``openbb.obb`` under an install-local lock so concurrent first-import builds wait safely."""
    spec = importlib.util.find_spec("openbb")
    if spec is None or spec.origin is None:
        msg = "No module named 'openbb'"
        raise ModuleNotFoundError(msg, name="openbb")
    with advisory_file_lock(Path(spec.origin).parent / ".mindroom-build.lock"):
        try:
            from openbb import obb
        except ValueError:
            # OpenBB's build installs a signal handler, which only the main thread may do,
            # so a build needed off the main thread runs in a child process before retrying.
            subprocess.run([sys.executable, "-c", "import openbb"], check=True)
            from openbb import obb
    return obb


class _OpenBBTools(Toolkit):
    """Fetch quotes, symbols, news, profiles, and price targets via OpenBB."""

    def __init__(
        self,
        obb: Any | None = None,  # noqa: ANN401 - optional caller-supplied OpenBB client
        openbb_pat: str | None = None,
        provider: _OpenBBProvider = "yfinance",
        enable_get_stock_price: bool | None = UNSET,
        enable_search_company_symbol: bool | None = UNSET,
        enable_get_company_news: bool | None = UNSET,
        enable_get_company_profile: bool | None = UNSET,
        enable_get_price_targets: bool | None = UNSET,
        all: bool | None = UNSET,  # noqa: A002 - matches ConfigField name
    ) -> None:
        self._obb = obb
        self._logged_in = False
        self.openbb_pat = openbb_pat.strip() if isinstance(openbb_pat, str) and openbb_pat.strip() else None
        self.provider: _OpenBBProvider = provider

        enable_all = resolve_bool_toggle(all, default=False)
        flag_map: tuple[tuple[str, bool | None, bool], ...] = (
            ("stock_price", enable_get_stock_price, True),
            ("search_symbol", enable_search_company_symbol, False),
            ("company_news", enable_get_company_news, False),
            ("company_profile", enable_get_company_profile, False),
            ("price_targets", enable_get_price_targets, False),
        )
        self._enabled_actions = tuple(
            action for action, flag, default in flag_map if enable_all or resolve_bool_toggle(flag, default=default)
        )
        tools = [self.openbb_get] if self._enabled_actions else []
        super().__init__(name="openbb_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``openbb_get`` schema and description to enabled actions."""
        if function.name != "openbb_get":
            return function
        narrowed = narrow_action_enum(function, self._enabled_actions)
        enabled = set(self._enabled_actions)
        unused_params = set()
        if not (enabled - {"search_symbol"}):
            unused_params.add("symbol")
        if "search_symbol" not in enabled:
            unused_params.add("company_name")
        if "company_news" not in enabled:
            unused_params.add("num_stories")
        props = {k: v for k, v in narrowed.parameters["properties"].items() if k not in unused_params}
        action_docs = {
            "stock_price": "- `stock_price`: quotes for `symbol` (comma-separated allowed, e.g. `AAPL,MSFT`).",
            "search_symbol": "- `search_symbol`: search ticker symbols matching `company_name`.",
            "company_news": "- `company_news`: recent news for `symbol` (up to `num_stories`).",
            "company_profile": "- `company_profile`: company overview for `symbol`.",
            "price_targets": "- `price_targets`: consensus price targets and recommendations for `symbol`.",
        }
        desc = "Fetch equity data from the configured OpenBB provider.\n\nActions:\n" + "\n".join(
            action_docs[a] for a in self._enabled_actions if a in action_docs
        )
        return replace(narrowed, description=desc, parameters={**narrowed.parameters, "properties": props})

    def _client(self) -> Any:  # noqa: ANN401 - OpenBB app object
        if self._obb is None:
            self._obb = _load_openbb_app()
        if self.openbb_pat and not self._logged_in:
            try:
                self._obb.account.login(pat=self.openbb_pat)
                self._logged_in = True
            except Exception:
                logger.exception("Error logging into OpenBB")
        return self._obb

    def _dispatch(
        self,
        action: _OpenBBAction,
        *,
        symbol: str | None,
        company_name: str | None,
        num_stories: int,
    ) -> str:
        obb = self._client()
        if action == "stock_price":
            target = require_non_empty(symbol, "symbol")
            rows = obb.equity.price.quote(symbol=target, provider=self.provider).to_polars().to_dicts()
            cleaned = [
                {
                    key: row.get(key)
                    for key in (
                        "symbol",
                        "last_price",
                        "currency",
                        "name",
                        "high",
                        "low",
                        "open",
                        "close",
                        "prev_close",
                        "volume",
                        "ma_50d",
                        "ma_200d",
                    )
                }
                for row in rows
            ]
            return json.dumps(cleaned, indent=2, default=str)
        if action == "search_symbol":
            query = require_non_empty(company_name, "company_name")
            result = obb.equity.search(query).to_polars()
            cleaned = [{"symbol": row.get("symbol"), "name": row.get("name")} for row in result.to_dicts()]
            return json.dumps(cleaned, indent=2, default=str)
        if action == "company_news":
            target = require_non_empty(symbol, "symbol")
            limit = max(1, num_stories)
            result = obb.news.company(symbol=target, provider=self.provider, limit=limit).to_polars()
            cleaned = [{k: v for k, v in row.items() if k != "images"} for row in result.to_dicts()]
            return json.dumps(cleaned[:limit], indent=2, default=str)
        if action == "company_profile":
            target = require_non_empty(symbol, "symbol")
            result = obb.equity.profile(symbol=target, provider=self.provider).to_polars()
            return json.dumps(result.to_dicts(), indent=2, default=str)
        if action == "price_targets":
            target = require_non_empty(symbol, "symbol")
            result = obb.equity.estimates.consensus(symbol=target, provider=self.provider).to_polars()
            return json.dumps(result.to_dicts(), indent=2, default=str)
        return f"Unsupported action: {action}"

    def openbb_get(
        self,
        action: _OpenBBAction,
        symbol: str | None = None,
        company_name: str | None = None,
        num_stories: int = 10,
    ) -> str:
        """Fetch equity data from the configured OpenBB provider.

        Actions:
        - `stock_price`: quotes for `symbol` (comma-separated allowed, e.g. `AAPL,MSFT`).
        - `search_symbol`: search ticker symbols matching `company_name`.
        - `company_news`: recent news for `symbol` (up to `num_stories`).
        - `company_profile`: company overview for `symbol`.
        - `price_targets`: consensus price targets and recommendations for `symbol`.
        """
        if (disabled := require_enabled_action(action, self._enabled_actions)) is not None:
            return disabled
        try:
            result = self._dispatch(action, symbol=symbol, company_name=company_name, num_stories=num_stories)
        except Exception as exc:
            return f"Error fetching OpenBB {action}: {exc}"
        return result


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
    function_names=("openbb_get",),
    action_scoped_grants=("openbb_get",),
)
def openbb_tools() -> type[Toolkit]:
    """Return OpenBB tools for financial data."""
    return _OpenBBTools
