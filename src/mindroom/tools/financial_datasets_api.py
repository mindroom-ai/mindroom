"""Financial Datasets API tool configuration and native implementation."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import require_non_empty

logger = get_logger(__name__)

_FinancialsAction = Literal[
    "income_statements",
    "balance_sheets",
    "cash_flow_statements",
    "segmented_financials",
    "financial_metrics",
    "company_info",
    "earnings",
    "insider_trades",
    "institutional_ownership",
    "sec_filings",
]
_MarketAction = Literal["stock_prices", "crypto_prices", "news", "search_tickers"]
_Period = Literal["annual", "quarterly", "ttm"]

_FINANCIALS_ENDPOINTS: dict[str, tuple[str, int | None, bool]] = {
    "income_statements": ("financials/income-statements", 10, True),
    "balance_sheets": ("financials/balance-sheets", 10, True),
    "cash_flow_statements": ("financials/cash-flow-statements", 10, True),
    "segmented_financials": ("financials/segmented", 10, True),
    "financial_metrics": ("financials/metrics", None, False),
    "company_info": ("company", None, False),
    "earnings": ("earnings", 10, False),
    "insider_trades": ("insider-trades", 50, False),
    "institutional_ownership": ("institutional-ownership", None, False),
    "sec_filings": ("sec-filings", 50, False),
}


class _FinancialDatasetsTools(Toolkit):
    """Query structured financial statements, filings, ownership, market prices, and crypto data."""

    def __init__(self, api_key: str | None = None, timeout: int = 30) -> None:
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.timeout = timeout
        self.base_url = "https://api.financialdatasets.ai"
        super().__init__(
            name="financial_datasets_tools",
            tools=[self.financial_datasets_financials, self.financial_datasets_market],
        )

    def _get(self, endpoint: str, params: dict[str, Any]) -> str:
        if not self.api_key:
            return "API key not set"
        url = f"{self.base_url}/{endpoint}"
        try:
            response = httpx.get(
                url,
                headers={"X-API-KEY": self.api_key},
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("financial_datasets_request_failed", endpoint=endpoint, error=str(exc))
            return f"Error making request to {url}: {exc}"
        return response.text

    def financial_datasets_financials(
        self,
        action: _FinancialsAction,
        ticker: str,
        period: _Period = "annual",
        form_type: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Fetch company financials, filings, earnings, and ownership for `ticker`.

        Actions:
        - `income_statements` / `balance_sheets` / `cash_flow_statements` / `segmented_financials`: statements for `period` (`annual`/`quarterly`/`ttm`, default `limit` 10).
        - `financial_metrics` / `company_info` / `institutional_ownership`: snapshot metrics, profile, or ownership.
        - `earnings` / `insider_trades`: reports (default `limit` 10 / 50).
        - `sec_filings`: SEC filings, optionally filtered by `form_type` (e.g. `10-K`, default `limit` 50).
        """
        spec = _FINANCIALS_ENDPOINTS.get(action)
        if spec is None:
            return f"Unsupported action: {action}"
        try:
            symbol = require_non_empty(ticker, "ticker")
        except ValueError as exc:
            return f"Error: {exc}"
        endpoint, default_limit, uses_period = spec
        params: dict[str, Any] = {"ticker": symbol}
        if uses_period:
            params["period"] = period
        effective_limit = default_limit if limit is None else limit
        if effective_limit is not None:
            params["limit"] = effective_limit
        if action == "sec_filings" and form_type:
            params["form_type"] = form_type
        return self._get(endpoint, params)

    def financial_datasets_market(
        self,
        action: _MarketAction,
        ticker: str | None = None,
        symbol: str | None = None,
        query: str | None = None,
        interval: str = "1d",
        limit: int | None = None,
    ) -> str:
        """Fetch stock/crypto prices, market news, or search tickers.

        Actions:
        - `stock_prices`: historical stock prices for `ticker` at `interval` (default `1d`, `limit` 100).
        - `crypto_prices`: historical crypto prices for `symbol` (e.g. `BTC`) at `interval` (default `limit` 100).
        - `news`: market news, optionally filtered by `ticker` (default `limit` 50).
        - `search_tickers`: search tickers matching `query` (default `limit` 10).
        """
        try:
            if action == "stock_prices":
                params: dict[str, Any] = {
                    "ticker": require_non_empty(ticker, "ticker"),
                    "interval": interval,
                    "limit": 100 if limit is None else limit,
                }
                return self._get("prices", params)
            if action == "crypto_prices":
                params = {
                    "symbol": require_non_empty(symbol, "symbol"),
                    "interval": interval,
                    "limit": 100 if limit is None else limit,
                }
                return self._get("crypto/prices", params)
            if action == "news":
                params = {"limit": 50 if limit is None else limit}
                if isinstance(ticker, str) and ticker.strip():
                    params["ticker"] = ticker.strip()
                return self._get("news", params)
            if action == "search_tickers":
                params = {
                    "query": require_non_empty(query, "query"),
                    "limit": 10 if limit is None else limit,
                }
                return self._get("search", params)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Unsupported action: {action}"


@register_tool_with_metadata(
    name="financial_datasets_api",
    file_access=ToolFileAccess.NONE,
    display_name="Financial Datasets API",
    description="Comprehensive financial data API for stocks, financial statements, SEC filings, and cryptocurrency",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="TrendingUp",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("FINANCIAL_DATASETS_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#financial_datasets_api",
    function_names=("financial_datasets_financials", "financial_datasets_market"),
    action_scoped_grants=("financial_datasets_financials", "financial_datasets_market"),
)
def financial_datasets_api_tools() -> type[Toolkit]:
    """Return Financial Datasets API tools for comprehensive financial data access."""
    return _FinancialDatasetsTools
