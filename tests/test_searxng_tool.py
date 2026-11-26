"""Tests for the unified SearxngTools toolkit."""

from __future__ import annotations

import json

import httpx
import pytest

from mindroom.custom_tools.searxng import SearxngTools
from tests.agent_tool_helpers import toolkit_offers


def test_searxng_general_web_search_and_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """General web search omits categories, strips trailing slash from host, and slices to max_results."""
    captured: list[tuple[str, dict[str, str], int]] = []

    def fake_get(url: str, *, params: dict[str, str], timeout: int) -> httpx.Response:
        captured.append((url, dict(params), timeout))
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={"query": params["q"], "results": [{"title": f"r{i}"} for i in range(6)]},
            request=request,
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    toolkit = SearxngTools(host="https://search.example.com///", timeout=15)

    payload = json.loads(toolkit.searxng_search("matrix protocol", max_results=2))

    assert captured == [("https://search.example.com/search", {"format": "json", "q": "matrix protocol"}, 15)]
    assert payload["results"] == [{"title": "r0"}, {"title": "r1"}]


def test_searxng_category_and_engines_and_fixed_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Category, configured engines, and fixed_max_results override max_results cleanly."""
    captured_params: list[dict[str, str]] = []

    def fake_get(url: str, *, params: dict[str, str], timeout: int) -> httpx.Response:
        assert timeout == 30
        captured_params.append(dict(params))
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json={"results": [{"title": f"r{i}"} for i in range(10)]},
            request=request,
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    toolkit = SearxngTools(
        host="https://search.example.com",
        engines=["duckduckgo", "wikipedia"],
        fixed_max_results=3,
    )

    payload = json.loads(toolkit.searxng_search("python 3.13", category="news", max_results=8))

    assert captured_params == [
        {
            "format": "json",
            "q": "python 3.13",
            "engines": "duckduckgo,wikipedia",
            "categories": "news",
        },
    ]
    assert len(payload["results"]) == 3


def test_searxng_clamps_max_results_to_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-positive max_results or fixed_max_results is clamped to at least 1."""

    def fake_get(url: str, *, params: dict[str, str], timeout: int) -> httpx.Response:
        del timeout
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, json={"results": [{"title": "a"}, {"title": "b"}]}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    toolkit = SearxngTools(host="https://search.example.com")

    payload = json.loads(toolkit.searxng_search("test", max_results=0))

    assert payload["results"] == [{"title": "a"}]


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.HTTPStatusError(
            "503 Service Unavailable",
            request=httpx.Request("GET", "https://search.example.com/search"),
            response=httpx.Response(503),
        ),
    ],
)
def test_searxng_http_errors_return_error_string(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Network and HTTP status errors return a clear error message instead of raising."""

    def fake_get(url: str, *, params: dict[str, str], timeout: int) -> httpx.Response:
        del url, params, timeout
        raise exc

    monkeypatch.setattr(httpx, "get", fake_get)
    toolkit = SearxngTools(host="https://search.example.com")

    result = toolkit.searxng_search("matrix")

    assert result.startswith("Error fetching results from searxng:")


def test_searxng_invalid_json_returns_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON responses return a clear error string."""

    def fake_get(url: str, *, params: dict[str, str], timeout: int) -> httpx.Response:
        del timeout
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, text="<html>not json</html>", request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    toolkit = SearxngTools(host="https://search.example.com")

    result = toolkit.searxng_search("matrix")

    assert result.startswith("Error fetching results from searxng:")


def test_searxng_schema_exposes_single_unified_function() -> None:
    """SearxngTools exposes only searxng_search with the expected category enum."""
    function = toolkit_offers(SearxngTools(host="https://search.example.com"), "searxng_search")

    assert set(function.parameters["required"]) == {"query"}
    category_schema = function.parameters["properties"]["category"]
    assert category_schema["enum"] == ["images", "it", "map", "music", "news", "science", "videos"]
