"""Tests for the native Jina Reader toolkit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from mindroom.tools.jina import jina_tools

if TYPE_CHECKING:
    import pytest


def _fake_response(url: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


def test_jina_omits_timeout_kwarg_when_timeout_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default timeout=None must omit timeout= so HTTPX keeps its default timeouts."""
    get_calls: list[dict[str, Any]] = []
    post_calls: list[dict[str, Any]] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        get_calls.append({"url": url, "kwargs": kwargs})
        return _fake_response(url, {"data": {"content": "hello"}})

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        post_calls.append({"url": url, "kwargs": kwargs})
        return _fake_response(url, {"data": [{"title": "result"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    tool = jina_tools()(api_key="jina-key", enable_search_query=True)

    read_result = tool.jina_read_url("https://example.com")
    search_result = tool.jina_search_query("mindroom")

    assert "hello" in read_result
    assert "result" in search_result
    assert len(get_calls) == 1
    assert "timeout" not in get_calls[0]["kwargs"]
    assert "X-Timeout" not in get_calls[0]["kwargs"]["headers"]
    assert len(post_calls) == 1
    assert "timeout" not in post_calls[0]["kwargs"]
    assert "X-Timeout" not in post_calls[0]["kwargs"]["headers"]


def test_jina_passes_explicit_timeout_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit timeout values propagate to both HTTPX and Jina's X-Timeout header."""
    get_calls: list[dict[str, Any]] = []
    post_calls: list[dict[str, Any]] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        get_calls.append({"url": url, "kwargs": kwargs})
        return _fake_response(url, {"data": {"content": "ok"}})

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        post_calls.append({"url": url, "kwargs": kwargs})
        return _fake_response(url, {"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    tool = jina_tools()(api_key="jina-key", timeout=15, enable_search_query=True)

    tool.jina_read_url("https://example.com")
    tool.jina_search_query("mindroom")

    assert get_calls[0]["kwargs"]["timeout"] == 15
    assert get_calls[0]["kwargs"]["headers"]["X-Timeout"] == "15"
    assert post_calls[0]["kwargs"]["timeout"] == 15
    assert post_calls[0]["kwargs"]["headers"]["X-Timeout"] == "15"
