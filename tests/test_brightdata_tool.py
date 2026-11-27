"""Tests for native Wave 4 consolidated toolkits (Bright Data, Oxylabs, Browserbase, Spider) and migration."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import requests

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.toolkits import adapt_toolkit
from mindroom.tools.brightdata import brightdata_tools
from mindroom.tools.browserbase import browserbase_tools
from mindroom.tools.oxylabs import oxylabs_tools
from mindroom.tools.spider import spider_tools

if TYPE_CHECKING:
    from pathlib import Path


def test_brightdata_youtube_videos_submits_correct_dataset_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """youtube_videos must submit the YouTube Videos dataset ID, not Booking's hotel dataset ID."""
    post_calls: list[dict[str, Any]] = []
    get_calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        post_calls.append({"url": url, "kwargs": kwargs})
        return SimpleNamespace(status_code=200, json=lambda: {"snapshot_id": "snap-yt-1"})

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        get_calls.append({"url": url, "kwargs": kwargs})
        return SimpleNamespace(status_code=200, json=lambda: [{"title": "Demo Video"}])

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    tool = brightdata_tools()(api_key="bd-secret")
    result = tool.brightdata(
        "web_data_feed",
        source_type="youtube_videos",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )

    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "https://api.brightdata.com/datasets/v3/trigger"
    assert post_calls[0]["kwargs"]["params"] == {
        "dataset_id": "gd_lk56epmy2i5g7lzu0k",
        "include_errors": "true",
    }
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == "https://api.brightdata.com/datasets/v3/snapshot/snap-yt-1"
    assert isinstance(result, str)
    assert json.loads(result) == [{"title": "Demo Video"}]


def test_brightdata_screenshot_returns_raw_png_bytes_in_tool_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Screenshot action must return a ToolValue carrying raw PNG bytes in an Image artifact."""
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRfake-png-payload"

    def fake_post(url: str, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
        del url, kwargs
        return SimpleNamespace(status_code=200, content=png_bytes, text="")

    monkeypatch.setattr(requests, "post", fake_post)

    tool = brightdata_tools()(api_key="bd-secret")
    result = tool.brightdata("screenshot", url="https://example.com")

    assert isinstance(result, ToolValue)
    assert len(result.images) == 1
    assert result.images[0].mime_type == "image/png"
    assert result.images[0].content == png_bytes
    assert result.images[0].original_prompt == "Screenshot of https://example.com"


def test_brightdata_browserbase_and_spider_toggle_narrowing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toggle switches narrow action enums, descriptions, and fields, and omit functions when all disabled."""
    bd_only_md = brightdata_tools()(
        api_key="k",
        enable_screenshot=False,
        enable_search_engine=False,
        enable_web_data_feed=False,
    )
    bd_fn = adapt_toolkit(bd_only_md).functions[0]
    assert bd_fn.parameters["properties"]["action"]["enum"] == ["scrape_markdown"]
    assert set(bd_fn.parameters["properties"]) == {"action", "url"}
    assert "`screenshot`" not in (bd_fn.description or "")
    bd_http_calls: list[str] = []
    monkeypatch.setattr(requests, "post", lambda url, **_kw: bd_http_calls.append(url))
    monkeypatch.setattr(requests, "get", lambda url, **_kw: bd_http_calls.append(url))
    assert "disabled" in str(bd_only_md.brightdata("screenshot", url="https://example.com")).lower()
    assert "cannot be used" in str(bd_only_md.brightdata("scrape_markdown", url="https://example.com", num_results=10))
    assert "cannot be used" in str(bd_only_md.brightdata("scrape_markdown", url="https://example.com", engine="google"))
    assert "cannot be used" in str(bd_only_md.brightdata("scrape_markdown", url="https://example.com", engine="bing"))
    bd_full = brightdata_tools()(api_key="k")
    assert "cannot be used" in str(bd_full.brightdata("search_engine", query="q", url="https://example.com"))
    assert bd_http_calls == []
    assert (
        list(
            brightdata_tools()(
                api_key="k",
                enable_scrape_markdown=False,
                enable_screenshot=False,
                enable_search_engine=False,
                enable_web_data_feed=False,
            ).functions,
        )
        == []
    )

    fake_bb_mod = ModuleType("browserbase")
    fake_bb_mod.Browserbase = lambda **_kwargs: SimpleNamespace()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "browserbase", fake_bb_mod)
    bb_default = browserbase_tools()(api_key="k", project_id="p")
    assert list(bb_default.functions) == ["browserbase", "screenshot"]
    bb_close_only = browserbase_tools()(
        api_key="k",
        project_id="p",
        enable_navigate_to=False,
        enable_screenshot=False,
        enable_get_page_content=False,
    )
    assert list(bb_close_only.functions) == ["browserbase"]
    bb_fn = adapt_toolkit(bb_close_only).functions[0]
    assert bb_fn.parameters["properties"]["action"]["enum"] == ["close_session"]
    assert set(bb_fn.parameters["properties"]) == {"action"}
    assert "`navigate`" not in (bb_fn.description or "")
    assert (
        "cannot be used"
        in json.loads(
            asyncio.run(bb_close_only.browserbase("close_session", connect_url="wss://example.test")),
        )["error"]
    )
    assert (
        "cannot be used"
        in json.loads(
            asyncio.run(bb_default.browserbase("get_page_content", url="https://example.test")),
        )["error"]
    )
    bb_screenshot_only = browserbase_tools()(
        api_key="k",
        project_id="p",
        enable_navigate_to=False,
        enable_get_page_content=False,
        enable_close_session=False,
    )
    assert list(bb_screenshot_only.functions) == ["screenshot"]

    spider_scrape = spider_tools()(enable_search=False, enable_crawl=False)
    spider_fn = adapt_toolkit(spider_scrape).functions[0]
    assert spider_fn.parameters["properties"]["action"]["enum"] == ["scrape"]
    assert set(spider_fn.parameters["properties"]) == {"action", "url"}
    assert "`search`" not in (spider_fn.description or "")
    assert "disabled" in spider_scrape.spider("search", query="x").lower()
    assert list(spider_tools()(enable_search=False, enable_scrape=False, enable_crawl=False).functions) == []


def test_oxylabs_and_spider_actions_preserve_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consolidated oxylabs and spider toolkits preserve validation and payload formats."""
    fake_ox_mod = ModuleType("oxylabs")

    ox_calls: list[str] = []

    class _FakeClient:
        def __init__(self, _u: str, _p: str) -> None:
            self.google = SimpleNamespace(
                scrape_search=lambda **_kw: (
                    ox_calls.append("google"),
                    SimpleNamespace(
                        results=[
                            SimpleNamespace(
                                content={"results": {"organic": [{"title": "T", "url": "https://x.test"}]}},
                            ),
                        ],
                    ),
                )[1],
            )
            self.amazon = SimpleNamespace(
                scrape_product=lambda **_kw: (
                    ox_calls.append("amazon_product"),
                    SimpleNamespace(
                        results=[SimpleNamespace(content={"title": "Widget", "price": 9.99, "currency": "USD"})],
                    ),
                )[1],
                scrape_search=lambda **_kw: (ox_calls.append("amazon_search"), SimpleNamespace(results=[]))[1],
            )
            self.universal = SimpleNamespace(
                scrape_url=lambda **_kw: (
                    ox_calls.append("scrape"),
                    SimpleNamespace(results=[SimpleNamespace(content="# Hi", status_code=200)]),
                )[1],
            )

    fake_ox_mod.RealtimeClient = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "oxylabs", fake_ox_mod)

    ox = oxylabs_tools()(username="u", password="p")  # noqa: S106
    google_out = json.loads(ox.oxylabs("search_google", query="mindroom"))
    assert google_out["tool"] == "search_google"
    assert google_out["results"][0]["title"] == "T"
    prod_out = json.loads(ox.oxylabs("amazon_product", asin="B07FZ8S74R"))
    assert prod_out["tool"] == "get_amazon_product"
    assert prod_out["product_info"]["found"] is True
    assert "Invalid ASIN" in json.loads(ox.oxylabs("amazon_product", asin="bad"))["error"]
    assert "Invalid URL" in json.loads(ox.oxylabs("scrape", url="ftp://bad"))["error"]
    ox_calls_before = len(ox_calls)
    assert "cannot be used" in json.loads(ox.oxylabs("scrape", url="https://example.com", domain_code="com"))["error"]
    assert "cannot be used" in json.loads(ox.oxylabs("scrape", url="https://example.com", domain_code="de"))["error"]
    assert "cannot be used" in json.loads(ox.oxylabs("search_google", query="q", render_javascript=False))["error"]
    assert "cannot be used" in json.loads(ox.oxylabs("search_google", query="q", markdown=True))["error"]
    assert len(ox_calls) == ox_calls_before

    fake_spider_mod = ModuleType("spider")
    seen_spider: list[tuple[str, str, dict[str, Any]]] = []

    class _FakeSpider:
        def search(self, query: str, options: dict[str, Any]) -> list[dict[str, str]]:
            seen_spider.append(("search", query, options))
            return [{"url": "https://example.com"}]

        def scrape_url(self, url: str, options: dict[str, Any]) -> dict[str, str]:
            seen_spider.append(("scrape", url, options))
            return {"content": "ok"}

        def crawl_url(self, url: str, options: dict[str, Any]) -> list[dict[str, str]]:
            seen_spider.append(("crawl", url, options))
            return [{"url": url}]

    fake_spider_mod.Spider = _FakeSpider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spider", fake_spider_mod)

    sp = spider_tools()(optional_params={"num": 99, "limit": 99})
    assert json.loads(sp.spider("search", query="python", max_results=3)) == [{"url": "https://example.com"}]
    assert json.loads(sp.spider("scrape", url="https://example.com")) == {"content": "ok"}
    assert json.loads(sp.spider("crawl", url="https://example.com", limit=4)) == [{"url": "https://example.com"}]
    assert seen_spider[0][2]["num"] == 3
    assert seen_spider[2][2]["limit"] == 4
    spider_calls_before = len(seen_spider)
    assert "cannot be used" in sp.spider("scrape", url="https://example.com", max_results=3)
    assert "cannot be used" in sp.spider("search", query="python", url="https://example.com")
    assert len(seen_spider) == spider_calls_before


def test_wave4_action_scoped_grants_and_fail_closed_migration(tmp_path: Path) -> None:
    """Wave 4 consolidated functions bind action-scoped grants and fail closed on retired approval/filter names."""
    registry = builtin_tool_registry()
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {
                "researcher": {
                    "display_name": "Researcher",
                    "tools": ["exa", "trafilatura", "brightdata", "oxylabs", "browserbase", "spider"],
                },
            },
        },
    )

    for toolkit_name, fn_name, first_action, second_action in (
        ("exa", "exa_lookup", "search", "contents"),
        ("trafilatura", "trafilatura_extract", "text", "batch"),
        ("brightdata", "brightdata", "scrape_markdown", "screenshot"),
        ("oxylabs", "oxylabs", "search_google", "scrape"),
        ("browserbase", "browserbase", "navigate", "close_session"),
        ("spider", "spider", "search", "crawl"),
    ):
        op_a = grant_operation(config, fn_name, {"action": first_action}, tools=registry, toolkit_name=toolkit_name)
        op_b = grant_operation(config, fn_name, {"action": second_action}, tools=registry, toolkit_name=toolkit_name)
        assert op_a is not None
        assert op_b is not None
        assert op_a != op_b
        assert (
            grant_operation(config, fn_name, {"url": "https://example.com"}, tools=registry, toolkit_name=toolkit_name)
            is None
        )

    bb_screenshot_op = grant_operation(
        config,
        "screenshot",
        {"path": str(tmp_path / "page.png")},
        tools=registry,
        toolkit_name="browserbase",
    )
    assert bb_screenshot_op is not None
    assert bb_screenshot_op.mcp_tool_name is None

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    # Separate Browserbase screenshot keeps its standalone approval rule boundary.
    loaded_screenshot_rule = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {"researcher": {"display_name": "Researcher", "tools": ["browserbase"]}},
            "tool_approval": {
                "default": "auto_approve",
                "rules": [{"match": "screenshot", "action": "require_approval"}],
            },
        },
        runtime_paths,
    )
    assert loaded_screenshot_rule.tool_approval.rules[0].match == "screenshot"
    for toolkit, retired, replacement in (
        ("exa", "search_exa", "exa_lookup"),
        ("trafilatura", "extract_batch", "trafilatura_extract"),
        ("brightdata", "scrape_as_markdown", "brightdata"),
        ("oxylabs", "get_amazon_product", "oxylabs"),
        ("browserbase", "navigate_to", "browserbase"),
        ("spider", "search_web", "spider"),
    ):
        with pytest.raises(ConfigRuntimeValidationError, match=replacement):
            Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "test-model"}},
                    "agents": {"researcher": {"display_name": "Researcher", "tools": [toolkit]}},
                    "tool_approval": {
                        "default": "auto_approve",
                        "rules": [{"match": retired, "action": "require_approval"}],
                    },
                },
                runtime_paths,
            )
        with pytest.raises(ConfigRuntimeValidationError, match=replacement) as exc_info:
            Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "test-model"}},
                    "agents": {
                        "researcher": {
                            "display_name": "Researcher",
                            "tools": [{toolkit: {"include_tools": [retired]}}],
                        },
                    },
                },
                runtime_paths,
            )
        override_error = str(exc_info.value)
        assert "tool_approval.rules" in override_error
        assert f"include_tools: [{replacement}]" not in override_error
        if toolkit == "oxylabs":
            assert "enable_*" not in override_error
            assert "no per-action allowlist filter" in override_error
        else:
            assert "enable_*" in override_error
