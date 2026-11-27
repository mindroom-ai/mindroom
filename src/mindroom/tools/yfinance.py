"""Yahoo Finance tool configuration and native implementation."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus, bool_config_field
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

_YFinanceAction = Literal[
    "stock_price",
    "company_info",
    "fundamentals",
    "income_statements",
    "financial_ratios",
    "analyst_recommendations",
    "company_news",
    "technical_indicators",
    "historical_prices",
]


def _clean_company_info(info: dict[str, Any]) -> dict[str, Any]:
    currency = info.get("currency", "USD")
    return {
        "Name": info.get("shortName"),
        "Symbol": info.get("symbol"),
        "Current Stock Price": f"{info.get('regularMarketPrice', info.get('currentPrice'))} {currency}",
        "Market Cap": f"{info.get('marketCap', info.get('enterpriseValue'))} {currency}",
        "Sector": info.get("sector"),
        "Industry": info.get("industry"),
        "Address": info.get("address1"),
        "City": info.get("city"),
        "State": info.get("state"),
        "Zip": info.get("zip"),
        "Country": info.get("country"),
        "EPS": info.get("trailingEps"),
        "P/E Ratio": info.get("trailingPE"),
        "52 Week Low": info.get("fiftyTwoWeekLow"),
        "52 Week High": info.get("fiftyTwoWeekHigh"),
        "50 Day Average": info.get("fiftyDayAverage"),
        "200 Day Average": info.get("twoHundredDayAverage"),
        "Website": info.get("website"),
        "Summary": info.get("longBusinessSummary"),
        "Analyst Recommendation": info.get("recommendationKey"),
        "Number Of Analyst Opinions": info.get("numberOfAnalystOpinions"),
        "Employees": info.get("fullTimeEmployees"),
        "Total Cash": info.get("totalCash"),
        "Free Cash flow": info.get("freeCashflow"),
        "Operating Cash flow": info.get("operatingCashflow"),
        "EBITDA": info.get("ebitda"),
        "Revenue Growth": info.get("revenueGrowth"),
        "Gross Margins": info.get("grossMargins"),
        "Ebitda Margins": info.get("ebitdaMargins"),
    }


def _fundamentals_payload(symbol: str, info: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "company_name": info.get("longName", ""),
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "market_cap": info.get("marketCap", "N/A"),
        "pe_ratio": info.get("forwardPE", "N/A"),
        "pb_ratio": info.get("priceToBook", "N/A"),
        "dividend_yield": info.get("dividendYield", "N/A"),
        "eps": info.get("trailingEps", "N/A"),
        "beta": info.get("beta", "N/A"),
        "52_week_high": info.get("fiftyTwoWeekHigh", "N/A"),
        "52_week_low": info.get("fiftyTwoWeekLow", "N/A"),
    }


class _YFinanceTools(Toolkit):
    """Fetch quotes, fundamentals, statements, news, and history from Yahoo Finance."""

    def __init__(
        self,
        enable_stock_price: bool | None = UNSET,
        enable_company_info: bool | None = UNSET,
        enable_stock_fundamentals: bool | None = UNSET,
        enable_income_statements: bool | None = UNSET,
        enable_key_financial_ratios: bool | None = UNSET,
        enable_analyst_recommendations: bool | None = UNSET,
        enable_company_news: bool | None = UNSET,
        enable_technical_indicators: bool | None = UNSET,
        enable_historical_prices: bool | None = UNSET,
        all: bool | None = UNSET,  # noqa: A002 - matches ConfigField name
        session: Any | None = None,  # noqa: ANN401 - optional caller-supplied HTTP session
    ) -> None:
        self.session = session
        enable_all = resolve_bool_toggle(all, default=False)
        flag_map: tuple[tuple[str, bool | None, bool], ...] = (
            ("stock_price", enable_stock_price, True),
            ("company_info", enable_company_info, False),
            ("fundamentals", enable_stock_fundamentals, False),
            ("income_statements", enable_income_statements, False),
            ("financial_ratios", enable_key_financial_ratios, False),
            ("analyst_recommendations", enable_analyst_recommendations, False),
            ("company_news", enable_company_news, False),
            ("technical_indicators", enable_technical_indicators, False),
            ("historical_prices", enable_historical_prices, False),
        )
        self._enabled_actions = tuple(
            action for action, flag, default in flag_map if enable_all or resolve_bool_toggle(flag, default=default)
        )
        tools = [self.yfinance_get] if self._enabled_actions else []
        super().__init__(name="yfinance_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``yfinance_get`` schema and description to enabled actions."""
        if function.name != "yfinance_get":
            return function
        narrowed = narrow_action_enum(function, self._enabled_actions)
        enabled = set(self._enabled_actions)
        unused_params = set()
        if not (enabled & {"technical_indicators", "historical_prices"}):
            unused_params.add("period")
        if "historical_prices" not in enabled:
            unused_params.add("interval")
        if "company_news" not in enabled:
            unused_params.add("num_stories")
        props = {k: v for k, v in narrowed.parameters["properties"].items() if k not in unused_params}
        action_docs = {
            "stock_price": "- `stock_price`: current/regular market stock price.",
            "company_info": "- `company_info`: company profile and key metrics.",
            "fundamentals": "- `fundamentals`: valuation and summary fundamentals.",
            "income_statements": "- `income_statements`: income statement table as JSON.",
            "financial_ratios": "- `financial_ratios`: full ticker info/ratios JSON.",
            "analyst_recommendations": "- `analyst_recommendations`: analyst recommendation history.",
            "company_news": "- `company_news`: recent news items (capped by `num_stories`).",
            "technical_indicators": "- `technical_indicators`: OHLCV history for `period` (default `3mo`).",
            "historical_prices": "- `historical_prices`: OHLCV history for `period` (default `1mo`) and `interval` (default `1d`).",
        }
        desc = "Fetch financial data for `symbol` from Yahoo Finance.\n\nActions:\n" + "\n".join(
            action_docs[a] for a in self._enabled_actions if a in action_docs
        )
        return replace(narrowed, description=desc, parameters={**narrowed.parameters, "properties": props})

    def _dispatch(
        self,
        action: _YFinanceAction,
        symbol: str,
        *,
        period: str | None,
        interval: str,
        num_stories: int,
    ) -> str:
        import yfinance as yf

        stock = yf.Ticker(symbol, session=self.session)
        handlers: dict[str, Any] = {
            "stock_price": lambda: (
                f"{price:.4f}"
                if (price := stock.info.get("regularMarketPrice", stock.info.get("currentPrice")))
                else f"Could not fetch current price for {symbol}"
            ),
            "company_info": lambda: (
                f"Could not fetch company info for {symbol}"
                if (info := stock.info) is None
                else json.dumps(_clean_company_info(info), indent=2)
            ),
            "fundamentals": lambda: json.dumps(_fundamentals_payload(symbol, stock.info), indent=2),
            "income_statements": lambda: str(stock.financials.to_json(orient="index")),
            "financial_ratios": lambda: json.dumps(stock.info, indent=2),
            "analyst_recommendations": lambda: str(stock.recommendations.to_json(orient="index")),
            "company_news": lambda: json.dumps(stock.news[: max(0, num_stories)], indent=2),
            "technical_indicators": lambda: str(stock.history(period=period or "3mo").to_json(orient="index")),
            "historical_prices": lambda: str(
                stock.history(period=period or "1mo", interval=interval).to_json(orient="index"),
            ),
        }
        handler = handlers.get(action)
        return f"Unsupported action: {action}" if handler is None else str(handler())

    def yfinance_get(
        self,
        action: _YFinanceAction,
        symbol: str,
        period: str | None = None,
        interval: str = "1d",
        num_stories: int = 3,
    ) -> str:
        """Fetch financial data for `symbol` from Yahoo Finance.

        Actions:
        - `stock_price`: current/regular market stock price.
        - `company_info`: company profile and key metrics.
        - `fundamentals`: valuation and summary fundamentals.
        - `income_statements`: income statement table as JSON.
        - `financial_ratios`: full ticker info/ratios JSON.
        - `analyst_recommendations`: analyst recommendation history.
        - `company_news`: recent news items (capped by `num_stories`).
        - `technical_indicators`: OHLCV history for `period` (default `3mo`).
        - `historical_prices`: OHLCV history for `period` (default `1mo`) and `interval` (default `1d`).
        """
        if (disabled := require_enabled_action(action, self._enabled_actions)) is not None:
            return disabled
        try:
            ticker = require_non_empty(symbol, "symbol")
            result = self._dispatch(action, ticker, period=period, interval=interval, num_stories=num_stories)
        except Exception as exc:
            return f"Error fetching {action} for {symbol}: {exc}"
        return result


@register_tool_with_metadata(
    name="yfinance",
    file_access=ToolFileAccess.NONE,
    display_name="Yahoo Finance",
    description="Get financial data and stock information from Yahoo Finance",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaChartLine",
    icon_color="text-purple-600",
    config_fields=[
        bool_config_field("enable_stock_price", "Enable Stock Price"),
        bool_config_field("enable_company_info", "Enable Company Info", default=False),
        bool_config_field("enable_stock_fundamentals", "Enable Stock Fundamentals", default=False),
        bool_config_field("enable_income_statements", "Enable Income Statements", default=False),
        bool_config_field("enable_key_financial_ratios", "Enable Key Financial Ratios", default=False),
        bool_config_field("enable_analyst_recommendations", "Enable Analyst Recommendations", default=False),
        bool_config_field("enable_company_news", "Enable Company News", default=False),
        bool_config_field("enable_technical_indicators", "Enable Technical Indicators", default=False),
        bool_config_field("enable_historical_prices", "Enable Historical Prices", default=False),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["yfinance"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#yfinance",
    function_names=("yfinance_get",),
    action_scoped_grants=("yfinance_get",),
)
def yfinance_tools() -> type[Toolkit]:
    """Return Yahoo Finance tools for financial data."""
    return _YFinanceTools
