"""Tests for native Wave 6 Batch 3 finance & weather toolkits (yfinance, openbb, financial_datasets_api, openweather)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
import yfinance as yf

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.registry import builtin_tool_registry

if TYPE_CHECKING:
    from pathlib import Path


class _FakeFrame:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_json(self, orient: str = "index") -> str:
        assert orient == "index"
        return json.dumps(self._payload)


class _FakePolars:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dicts(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows]


class _FakeOBBResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_polars(self) -> _FakePolars:
        return _FakePolars(self._rows)


def test_yfinance_actions_toggles_and_schema_narrowing(monkeypatch: pytest.MonkeyPatch) -> None:
    """YFinanceTools narrows default actions to stock_price, respects None toggles, and dispatches all actions."""
    yf_cls = cast("Any", builtin_tool_registry().factory("yfinance")())

    # Default enables only stock_price.
    default_tool = yf_cls()
    adapted = adapt_toolkit(default_tool)
    assert adapted.functions[0].parameters["properties"]["action"]["enum"] == ["stock_price"]
    assert "disabled" in default_tool.yfinance_get("company_info", "AAPL")

    # Explicit None disables stock_price too -> zero tools offered.
    assert yf_cls(enable_stock_price=None).functions == {}

    class _FakeTicker:
        def __init__(self, symbol: str, session: object = None) -> None:
            self.symbol = symbol
            self.session = session
            self.info = {
                "shortName": "Apple Inc.",
                "longName": "Apple Inc.",
                "symbol": symbol,
                "regularMarketPrice": 182.5,
                "currency": "USD",
                "marketCap": 2_800_000_000_000,
                "sector": "Technology",
            }
            self.financials = _FakeFrame({"revenue": 100})
            self.recommendations = _FakeFrame({"buy": 12})
            self.news = [{"title": "Story 1"}, {"title": "Story 2"}, {"title": "Story 3"}]

        def history(self, *, period: str, interval: str = "1d") -> _FakeFrame:
            return _FakeFrame({"period": period, "interval": interval})

    monkeypatch.setattr(yf, "Ticker", _FakeTicker)

    full = yf_cls(all=True)
    assert full.yfinance_get("stock_price", "AAPL") == "182.5000"
    assert json.loads(full.yfinance_get("company_info", "AAPL"))["Name"] == "Apple Inc."
    assert json.loads(full.yfinance_get("fundamentals", "AAPL"))["sector"] == "Technology"
    assert json.loads(full.yfinance_get("income_statements", "AAPL")) == {"revenue": 100}
    assert json.loads(full.yfinance_get("analyst_recommendations", "AAPL")) == {"buy": 12}
    assert len(json.loads(full.yfinance_get("company_news", "AAPL", num_stories=2))) == 2
    assert json.loads(full.yfinance_get("technical_indicators", "AAPL"))["period"] == "3mo"
    assert json.loads(full.yfinance_get("historical_prices", "AAPL", period="5d", interval="1h")) == {
        "period": "5d",
        "interval": "1h",
    }


def test_openbb_scoped_pat_and_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenBBTools never reads ambient OPENBB_PAT, logs in only with explicit PAT, and handles news without images."""
    monkeypatch.setenv("OPENBB_PAT", "ambient-pat")
    openbb_cls = cast("Any", builtin_tool_registry().factory("openbb")())

    logins: list[str] = []
    fake_obb = SimpleNamespace(
        account=SimpleNamespace(login=lambda *, pat: logins.append(pat)),
        equity=SimpleNamespace(
            price=SimpleNamespace(
                quote=lambda *, symbol, provider: _FakeOBBResult(
                    [{"symbol": symbol, "last_price": 150.0, "provider": provider}],
                ),
            ),
            search=lambda query: _FakeOBBResult([{"symbol": "AAPL", "name": f"{query} Inc."}]),
            profile=lambda *, symbol, provider: _FakeOBBResult([{"symbol": symbol, "provider": provider}]),
            estimates=SimpleNamespace(
                consensus=lambda *, symbol, provider: _FakeOBBResult(
                    [{"symbol": symbol, "provider": provider, "target": 200}],
                ),
            ),
        ),
        news=SimpleNamespace(
            company=lambda *, symbol, provider, limit: _FakeOBBResult(
                [{"title": f"{symbol} {provider} news", "images": ["drop-me"]}, {"title": f"{symbol} plain"}][:limit],
            ),
        ),
    )

    # Without explicit openbb_pat, never logs in even though OPENBB_PAT is in host env.
    no_pat = openbb_cls(obb=fake_obb, all=True)
    quote = json.loads(no_pat.openbb_get("stock_price", symbol="AAPL"))
    assert quote[0]["last_price"] == 150.0
    assert logins == []

    # News items both with and without "images" succeed cleanly.
    news = json.loads(no_pat.openbb_get("company_news", symbol="AAPL", num_stories=2))
    assert news == [{"title": "AAPL yfinance news"}, {"title": "AAPL plain"}]
    assert json.loads(no_pat.openbb_get("search_symbol", company_name="Apple"))[0]["symbol"] == "AAPL"
    assert json.loads(no_pat.openbb_get("company_profile", symbol="AAPL"))[0]["provider"] == "yfinance"
    assert json.loads(no_pat.openbb_get("price_targets", symbol="AAPL"))[0]["target"] == 200

    # With explicit openbb_pat, logs in once.
    with_pat = openbb_cls(obb=fake_obb, openbb_pat="scoped-pat")
    with_pat.openbb_get("stock_price", symbol="MSFT")
    with_pat.openbb_get("stock_price", symbol="NVDA")
    assert logins == ["scoped-pat"]

    # Explicit None disables stock_price.
    assert openbb_cls(obb=fake_obb, enable_get_stock_price=None).functions == {}


def test_financial_datasets_api_scoped_key_and_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """FinancialDatasetsTools fails closed without scoped key and sends X-API-KEY to the right endpoints."""
    monkeypatch.setenv("FINANCIAL_DATASETS_API_KEY", "ambient-key")
    fd_cls = cast("Any", builtin_tool_registry().factory("financial_datasets_api")())

    calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def fake_get(url: str, *, headers: dict[str, str], params: dict[str, object], timeout: int) -> httpx.Response:
        assert timeout > 0
        calls.append((url, dict(params), dict(headers)))
        return httpx.Response(200, json={"ok": True, "url": url, "params": params}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    unconfigured = fd_cls()
    assert unconfigured.financial_datasets_financials("income_statements", ticker="AAPL") == "API key not set"
    assert unconfigured.financial_datasets_market("stock_prices", ticker="AAPL") == "API key not set"
    assert calls == []

    tool = fd_cls(api_key="scoped-fd-key", timeout=15)
    res_fin = json.loads(
        tool.financial_datasets_financials("sec_filings", ticker="AAPL", form_type="10-K", limit=5),
    )
    assert res_fin["url"].endswith("/sec-filings")
    assert res_fin["params"] == {"ticker": "AAPL", "limit": 5, "form_type": "10-K"}
    assert calls[-1][2]["X-API-KEY"] == "scoped-fd-key"

    res_mkt = json.loads(tool.financial_datasets_market("crypto_prices", symbol="BTC", interval="1h", limit=12))
    assert res_mkt["url"].endswith("/crypto/prices")
    assert res_mkt["params"] == {"symbol": "BTC", "interval": "1h", "limit": 12}

    res_search = json.loads(tool.financial_datasets_market("search_tickers", query="Apple"))
    assert res_search["params"] == {"query": "Apple", "limit": 10}


def test_openweather_scoped_key_redaction_and_toggles(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenWeatherTools rejects ambient-only keys, geocodes then fetches, redacts key on error, and obeys toggles."""
    monkeypatch.setenv("OPENWEATHER_API_KEY", "ambient-owm")
    owm_cls = cast("Any", builtin_tool_registry().factory("openweather")())

    with pytest.raises(ValueError, match="OpenWeather API key is required"):
        owm_cls()

    api_token = "owm-test-val"  # noqa: S105

    def fake_get(url: str, *, params: dict[str, object], timeout: int) -> httpx.Response:
        assert timeout > 0
        assert params["appid"] == api_token
        if url.endswith("/direct"):
            if params["q"] == "ErrorTown":
                req = httpx.Request("GET", f"{url}?q=ErrorTown&appid={api_token}")
                err_msg = f"failed calling {req.url}"
                raise httpx.ConnectError(err_msg, request=req)
            return httpx.Response(
                200,
                json=[{"name": "London", "country": "GB", "lat": 51.5, "lon": -0.1}],
                request=httpx.Request("GET", url),
            )
        if url.endswith("/weather"):
            return httpx.Response(
                200,
                json={"main": {"temp": 14.2}, "units": params["units"]},
                request=httpx.Request("GET", url),
            )
        if url.endswith("/forecast"):
            return httpx.Response(200, json={"cnt": params["cnt"], "list": []}, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"list": [{"main": {"aqi": 2}}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    tool = owm_cls(api_key=api_token, units="imperial")
    current = json.loads(tool.openweather_get("current_weather", "London"))
    assert current["location_name"] == "London"
    assert current["country"] == "GB"
    assert current["units"] == "imperial"

    forecast = json.loads(tool.openweather_get("forecast", "London", days=99))
    assert forecast["cnt"] == 40

    pollution = json.loads(tool.openweather_get("air_pollution", "London"))
    assert pollution["list"][0]["main"]["aqi"] == 2

    # Key is redacted from transport error text.
    err = json.loads(tool.openweather_get("geocode", "ErrorTown"))
    assert api_token not in err["error"]
    assert "[REDACTED]" in err["error"]

    # Explicit None disables actions and narrows enum.
    limited = owm_cls(
        api_key=api_token,
        enable_current_weather=True,
        enable_forecast=None,
        enable_air_pollution=None,
        enable_geocoding=False,
    )
    adapted = adapt_toolkit(limited)
    assert adapted.functions[0].parameters["properties"]["action"]["enum"] == ["current_weather"]


def test_wave6_batch3_action_scoped_grants_and_fail_closed_migration(tmp_path: Path) -> None:
    """Batch 3 consolidated tools scope timed grants per action and fail closed on retired approval rules."""
    registry = builtin_tool_registry()
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {
                "analyst": {
                    "display_name": "Analyst",
                    "tools": ["yfinance", "openbb", "financial_datasets_api", "openweather"],
                },
            },
        },
    )

    for toolkit, fn, a1, a2 in (
        ("yfinance", "yfinance_get", "stock_price", "company_news"),
        ("openbb", "openbb_get", "stock_price", "price_targets"),
        ("financial_datasets_api", "financial_datasets_financials", "income_statements", "sec_filings"),
        ("financial_datasets_api", "financial_datasets_market", "stock_prices", "news"),
        ("openweather", "openweather_get", "current_weather", "forecast"),
    ):
        op1 = grant_operation(config, fn, {"action": a1}, tools=registry, toolkit_name=toolkit)
        op2 = grant_operation(config, fn, {"action": a2}, tools=registry, toolkit_name=toolkit)
        assert op1 is not None
        assert op2 is not None
        assert op1 != op2

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    for toolkit, retired in (
        ("yfinance", "get_current_stock_price"),
        ("openbb", "get_price_targets"),
        ("financial_datasets_api", "get_income_statements"),
        ("financial_datasets_api", "get_crypto_prices"),
        ("openweather", "get_forecast"),
    ):
        with pytest.raises(ConfigRuntimeValidationError, match=retired):
            Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "test-model"}},
                    "agents": {"analyst": {"display_name": "Analyst", "tools": [toolkit]}},
                    "tool_approval": {
                        "default": "auto_approve",
                        "rules": [{"match": retired, "action": "require_approval"}],
                    },
                },
                runtime_paths,
            )
