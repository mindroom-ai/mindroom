"""Offline tests for native Wave 2 search toolkits (DDGS, BaiduSearch, Serper, and SerpApi)."""

from __future__ import annotations

import inspect
import json
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import quote, quote_plus

import httpx
import pytest
import serpapi
from ddgs.exceptions import DDGSException, TimeoutException

from mindroom.custom_tools import baidusearch as baidusearch_module
from mindroom.custom_tools import ddgs_search as ddgs_search_module
from mindroom.tool_system.authoring import Toolkit, authored_description
from mindroom.tool_system.tool_schema import tool_description, tool_parameters
from mindroom.tools.baidusearch import baidusearch_tools
from mindroom.tools.duckduckgo import duckduckgo_tools
from mindroom.tools.googlesearch import googlesearch_tools
from mindroom.tools.serpapi import serpapi_tools
from mindroom.tools.serper import serper_tools


@pytest.mark.parametrize(
    ("factory", "expected_name", "expected_functions"),
    [
        (duckduckgo_tools, "websearch", ["web_search", "search_news"]),
        (googlesearch_tools, "websearch", ["web_search", "search_news"]),
        (baidusearch_tools, "baidusearch", ["baidu_search"]),
        (serper_tools, "serper_tools", ["search_web", "search_news", "search_scholar", "scrape_webpage"]),
        (serpapi_tools, "serpapi_tools", ["search_google"]),
    ],
)
def test_wave2_search_toolkits_are_native_with_preserved_names_and_order(
    factory: Any,  # noqa: ANN401
    expected_name: str,
    expected_functions: list[str],
) -> None:
    """Each scoped factory returns a native Toolkit subclass with its preserved name and default function order."""
    tool_class = factory()
    toolkit = tool_class()

    assert issubclass(tool_class, Toolkit)
    assert toolkit.name == expected_name
    assert list(toolkit.functions) == expected_functions


def test_duckduckgo_and_googlesearch_share_one_ddgs_implementation() -> None:
    """DuckDuckGo and Google Search share their web and news search methods."""
    ddg_cls = duckduckgo_tools()
    google_cls = googlesearch_tools()

    assert ddg_cls.web_search is google_cls.web_search
    assert ddg_cls.search_news is google_cls.search_news


def test_wave2_search_tools_preserve_derived_descriptions_and_schemas() -> None:
    """Native search tool methods preserve their descriptions and non-null optional schemas."""
    baidu_fn = baidusearch_tools()().functions["baidu_search"]
    assert (authored_description(baidu_fn) or tool_description(baidu_fn)) == "Execute Baidu search and return results"
    baidu_schema = tool_parameters(baidu_fn)
    assert baidu_schema["required"] == ["query"]
    assert set(baidu_schema["properties"]) == {"query", "max_results", "language"}

    serper = serper_tools()()
    for name in ("search_web", "search_news", "search_scholar"):
        schema = tool_parameters(serper.functions[name])
        assert schema["required"] == ["query"]
        assert schema["properties"]["num_results"]["type"] == "integer"

    scrape_schema = tool_parameters(serper.functions["scrape_webpage"])
    assert scrape_schema["required"] == ["url"]
    assert scrape_schema["properties"]["markdown"]["type"] == "boolean"

    serpapi_toolkit = serpapi_tools()(all=True)
    assert tool_parameters(serpapi_toolkit.functions["search_google"])["required"] == ["query"]
    assert tool_parameters(serpapi_toolkit.functions["search_youtube"])["required"] == ["query"]


def test_ddgs_search_tools_backends_options_and_modifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """DDGS search forwards client config, backend choice, and one modifier for each query."""
    client_inits: list[dict[str, Any]] = []
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeDDGS:
        def __init__(
            self,
            proxy: str | None = None,
            timeout: int | None = 5,
            *,
            verify: bool = True,
        ) -> None:
            client_inits.append({"proxy": proxy, "timeout": timeout, "verify": verify})

        def __enter__(self) -> FakeDDGS:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def text(self, **kwargs: Any) -> list[dict[str, str]]:  # noqa: ANN401
            calls.append(("text", kwargs))
            return [{"title": "Café", "href": "https://example.com"}]

        def news(self, **kwargs: Any) -> list[dict[str, str]]:  # noqa: ANN401
            calls.append(("news", kwargs))
            return [{"title": "Breaking", "url": "https://news.example.com"}]

    monkeypatch.setattr(ddgs_search_module, "DDGS", FakeDDGS)

    ddg = duckduckgo_tools()(
        modifier="site:example.com",
        fixed_max_results=3,
        proxy="http://proxy.internal:8080",
        timeout=25,
        verify_ssl=False,
        timelimit="w",
        region="us-en",
    )
    web_raw = ddg.web_search("coffee", max_results=9)
    news_raw = ddg.search_news("coffee", max_results=9)

    assert "Café" in web_raw
    assert json.loads(web_raw) == [{"title": "Café", "href": "https://example.com"}]
    assert json.loads(news_raw) == [{"title": "Breaking", "url": "https://news.example.com"}]

    ddg_custom = duckduckgo_tools()(backend="brave")
    ddg_custom.web_search("rust")

    google = googlesearch_tools()()
    google.web_search("python", max_results=4)
    google.search_news("python", max_results=2)

    assert client_inits[0] == {"proxy": "http://proxy.internal:8080", "timeout": 25, "verify": False}
    assert client_inits[-1] == {"proxy": None, "timeout": 10, "verify": True}
    assert calls == [
        (
            "text",
            {
                "query": "site:example.com coffee",
                "max_results": 3,
                "backend": "duckduckgo",
                "timelimit": "w",
                "region": "us-en",
            },
        ),
        (
            "news",
            {
                "query": "site:example.com coffee",
                "max_results": 3,
                "backend": "duckduckgo",
                "timelimit": "w",
                "region": "us-en",
            },
        ),
        ("text", {"query": "rust", "max_results": 5, "backend": "brave"}),
        ("text", {"query": "python", "max_results": 4, "backend": "google"}),
        ("news", {"query": "python", "max_results": 2, "backend": "google"}),
    ]

    google_cls: Any = googlesearch_tools()
    assert "backend" not in inspect.signature(google_cls).parameters
    with pytest.raises(TypeError, match="backend"):
        google_cls(backend="bing")


@pytest.mark.parametrize(("fixed_limit", "requested_limit"), [(0, 5), (None, 0), (None, -1)])
def test_search_tools_skip_the_sdk_for_nonpositive_limits(
    monkeypatch: pytest.MonkeyPatch,
    fixed_limit: int | None,
    requested_limit: int,
) -> None:
    """A nonpositive effective limit returns no results without touching either search SDK."""

    def unexpected_search(**_kwargs: object) -> None:
        pytest.fail("search SDK called with a nonpositive limit")

    monkeypatch.setattr(ddgs_search_module, "DDGS", unexpected_search)
    monkeypatch.setattr(baidusearch_module, "search", unexpected_search)
    ddg = duckduckgo_tools()(fixed_max_results=fixed_limit)
    baidu = baidusearch_tools()(fixed_max_results=fixed_limit)

    assert ddg.web_search("coffee", max_results=requested_limit) == "[]"
    assert ddg.search_news("coffee", max_results=requested_limit) == "[]"
    assert baidu.baidu_search("coffee", max_results=requested_limit) == "[]"


@pytest.mark.parametrize("error", [DDGSException("No results found."), TimeoutException("Backend timed out")])
def test_ddgs_search_returns_structured_backend_errors(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Backend errors keep the same JSON shape and effective query for both tools."""
    sdk = MagicMock()
    sdk.__enter__.return_value.text.side_effect = error
    sdk.__enter__.return_value.news.side_effect = error
    monkeypatch.setattr(ddgs_search_module, "DDGS", lambda **_kwargs: sdk)
    ddg = duckduckgo_tools()(modifier="site:example.com")
    expected = {"error": type(error).__name__, "message": str(error), "query": "site:example.com coffee", "results": []}

    assert json.loads(ddg.web_search("coffee")) == expected
    assert json.loads(ddg.search_news("coffee")) == expected


def test_ddgs_search_tools_validation_and_toggles() -> None:
    """Both DDGS toolkits validate timelimit and honor enable_search and enable_news flags."""
    for factory in (duckduckgo_tools, googlesearch_tools):
        tool_class = factory()
        with pytest.raises(ValueError, match="Invalid timelimit"):
            tool_class(timelimit="hour")

        assert list(tool_class(enable_search=False).functions) == ["search_news"]
        assert list(tool_class(enable_news=False).functions) == ["web_search"]
        assert list(tool_class(enable_search=False, enable_news=False).functions) == []


def test_baidusearch_formatting_language_normalization_and_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """BaiduSearch formats results, normalizes languages safely, and handles None results."""
    seen: list[tuple[str, int]] = []

    def fake_search(*, keyword: str, num_results: int) -> list[dict[str, str]] | None:
        seen.append((keyword, num_results))
        if not keyword:
            return None
        return [
            {"title": "百度搜索结果", "url": "https://example.cn/1", "abstract": "Summary 1"},
            {"title": "Result 2"},
        ]

    monkeypatch.setattr(baidusearch_module, "search", fake_search)

    assert baidusearch_module._normalize_language("zh") == "zh"
    assert baidusearch_module._normalize_language("English") == "en"
    assert baidusearch_module._normalize_language("cmn") == "zh"
    assert baidusearch_module._normalize_language("not-a-language") == "zh"

    toolkit = baidusearch_tools()(fixed_max_results=3, fixed_language="English")
    raw = toolkit.baidu_search("python", max_results=10, language="fr")
    assert "百度搜索结果" in raw
    payload = json.loads(raw)

    assert payload == [
        {"title": "百度搜索结果", "url": "https://example.cn/1", "abstract": "Summary 1", "rank": "1"},
        {"title": "Result 2", "url": "", "abstract": "", "rank": "2"},
    ]
    assert json.loads(toolkit.baidu_search("")) == []
    assert seen == [("python", 3), ("", 3)]


def test_baidusearch_enable_flags_and_all() -> None:
    """BaiduSearchTools honors enable_baidu_search and the all override."""
    tool_class = baidusearch_tools()

    assert list(tool_class(enable_baidu_search=False).functions) == []
    assert list(tool_class(enable_baidu_search=False, all=True).functions) == ["baidu_search"]


def test_serper_all_endpoints_headers_payload_and_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serper posts to all four endpoints over httpx with expected headers, payload, and env key fallback."""
    captured: list[tuple[str, dict[str, str], dict[str, Any], dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append((str(request.url), dict(request.headers), body, dict(request.extensions["timeout"])))
        return httpx.Response(200, text=json.dumps({"endpoint": request.url.path, "received": body}))

    monkeypatch.setenv("SERPER_API_KEY", "serper-env-key")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        toolkit = serper_tools()(location="nl", language="nl", num_results=7, date_range="qdr:w", timeout=15)

        web = json.loads(toolkit.search_web("python"))
        news = json.loads(toolkit.search_news("ai", num_results=3))
        scholar = json.loads(toolkit.search_scholar("transformers"))
        scraped = json.loads(toolkit.scrape_webpage("https://example.com", markdown=True))

    assert web["endpoint"] == "/search"
    assert news["endpoint"] == "/news"
    assert scholar["endpoint"] == "/scholar"
    assert scraped["endpoint"] == "/"

    assert [url for url, _, _, _ in captured] == [
        "https://google.serper.dev/search",
        "https://google.serper.dev/news",
        "https://google.serper.dev/scholar",
        "https://scrape.serper.dev",
    ]
    for _, headers, _, timeout_ext in captured:
        assert headers["x-api-key"] == "serper-env-key"
        assert headers["content-type"] == "application/json"
        assert timeout_ext["read"] == 15

    assert [body for _, _, body, _ in captured] == [
        {"q": "python", "num": 7, "tbs": "qdr:w", "gl": "nl", "hl": "nl"},
        {"q": "ai", "num": 3, "tbs": "qdr:w", "gl": "nl", "hl": "nl"},
        {"q": "transformers", "num": 7, "tbs": "qdr:w", "gl": "nl", "hl": "nl"},
        {"url": "https://example.com", "includeMarkdown": True, "tbs": "qdr:w", "gl": "nl", "hl": "nl"},
    ]


def test_serper_error_paths_and_toggles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serper validates inputs, handles missing API keys and HTTP/JSON failures, and honors enable flags."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    no_key_toolkit = serper_tools()()

    assert json.loads(no_key_toolkit.search_web("")) == {"error": "Please provide a query to search for"}
    assert json.loads(no_key_toolkit.search_news("")) == {"error": "Please provide a query to search for news"}
    assert json.loads(no_key_toolkit.search_scholar("")) == {
        "error": "Please provide a query to search for academic papers",
    }
    assert json.loads(no_key_toolkit.scrape_webpage("")) == {"error": "Please provide a URL to scrape"}
    assert json.loads(no_key_toolkit.search_web("python")) == {"error": "Please provide a Serper API key"}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news":
            return httpx.Response(500, text="server error")
        return httpx.Response(200, text="<html>not json</html>")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        toolkit = serper_tools()(api_key="serper-key")

        assert "error" in json.loads(toolkit.search_news("outage"))
        assert "error" in json.loads(toolkit.search_web("bad-json"))

    tool_class = serper_tools()
    only_scrape = tool_class(enable_search=False, enable_search_news=False, enable_search_scholar=False)
    assert list(only_scrape.functions) == ["scrape_webpage"]

    all_enabled = tool_class(
        enable_search=False,
        enable_search_news=False,
        enable_search_scholar=False,
        enable_scrape_webpage=False,
        all=True,
    )
    assert list(all_enabled.functions) == ["search_web", "search_news", "search_scholar", "scrape_webpage"]


def test_serpapi_google_youtube_env_fallback_and_key_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """SerpApi filters Google and YouTube results, handles validation, and redacts API keys on error."""
    google_params: list[dict[str, Any]] = []
    youtube_params: list[dict[str, Any]] = []

    class FakeGoogleSearch:
        def __init__(self, params: dict[str, Any]) -> None:
            google_params.append(params)
            self._params = params

        def get_dict(self) -> dict[str, Any]:
            if self._params["q"] == "boom":
                key = self._params["api_key"]
                msg = (
                    f"Request failed: https://serpapi.com/search?api_key={key}"
                    f"&enc1={quote(key, safe='')}&enc2={quote_plus(key)}"
                )
                raise RuntimeError(msg)
            return {
                "organic_results": [{"title": "MindRoom"}],
                "knowledge_graph": {"title": "MindRoom"},
                "ignored_field": "drop me",
            }

    class FakeYoutubeSearch:
        def __init__(self, params: dict[str, Any]) -> None:
            youtube_params.append(params)

        def get_dict(self) -> dict[str, Any]:
            return {"video_results": [{"title": "Demo"}], "channel_results": [{"title": "Official"}]}

    monkeypatch.setattr(serpapi, "GoogleSearch", FakeGoogleSearch)
    monkeypatch.setattr(serpapi, "YoutubeSearch", FakeYoutubeSearch)
    monkeypatch.delenv("SERP_API_KEY", raising=False)

    no_key = serpapi_tools()()
    assert no_key.search_google("mindroom") == "Please provide an API key"
    assert no_key.search_youtube("mindroom") == "Please provide an API key"

    test_key = "serp key+1/2"
    monkeypatch.setenv("SERP_API_KEY", test_key)
    toolkit = serpapi_tools()(enable_search_youtube=True)

    assert toolkit.search_google("") == "Please provide a query to search for"
    assert toolkit.search_youtube("") == "Please provide a query to search for"

    google_result = json.loads(toolkit.search_google("mindroom", num_results=5))
    assert google_result == {
        "search_results": [{"title": "MindRoom"}],
        "recipes_results": "",
        "shopping_results": "",
        "knowledge_graph": {"title": "MindRoom"},
        "related_questions": "",
    }
    youtube_result = json.loads(toolkit.search_youtube("matrix"))
    assert youtube_result == {
        "video_results": [{"title": "Demo"}],
        "movie_results": "",
        "channel_results": [{"title": "Official"}],
    }

    error_text = toolkit.search_google("boom")
    assert error_text.startswith("Error searching for the query boom:")
    assert test_key not in error_text
    assert quote(test_key, safe="") not in error_text
    assert quote_plus(test_key) not in error_text
    assert "[REDACTED]" in error_text

    assert google_params[0] == {"q": "mindroom", "num": 5, "api_key": test_key}
    assert youtube_params[0] == {"search_query": "matrix", "api_key": test_key}


def test_serpapi_enable_flags_and_all() -> None:
    """SerpApiTools defaults to Google only and honors YouTube and all switches."""
    tool_class = serpapi_tools()

    assert list(tool_class().functions) == ["search_google"]
    assert list(tool_class(enable_search_youtube=True).functions) == ["search_google", "search_youtube"]
    assert list(tool_class(enable_search_google=False).functions) == []
    assert list(tool_class(enable_search_google=False, enable_search_youtube=False, all=True).functions) == [
        "search_google",
        "search_youtube",
    ]
